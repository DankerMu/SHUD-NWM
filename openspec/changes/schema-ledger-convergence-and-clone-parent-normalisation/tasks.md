## Risk packs

- **Migration / data backfill: selected.** Three forward migrations (D1–D3), one of which drops a schema on production. Covered by 2.1–2.3, the node-27 disposable-DB run (3.3), CI SQL Migration Dry Run (4.1) and the production apply receipt (5.x).
- **Error handling / rollback / partial outputs: selected.** `000064` is fail-closed (I3); a failed `000063` must be safe to rerun (D2); the runner refuses a ledger/disk mismatch before applying anything (D4). Rollback is the pre-apply `pg_dump` (D3).
- **Schema / columns / units / field names: selected.** `hydro.run_status` gains `frequency_done` on fresh databases (D1). No API field changes.
- **Public API / CLI / script entry: selected.** `python -m packages.common.migrate` gains an exit-1 refusal (D4). Its callers are `Makefile:20`, `scripts/run_qhh_cycle.sh:425`, `scripts/run_qhh_backend_smoke.sh:157` and the runbooks. Covered by 2.5 (unit + real-DB + `tests/test_migrate_lock_timeout.py`) and 3.2. No HTTP route changes.
- **Legacy compatibility / examples: selected.** I1: no existing migration file changes. The 7 retired ledger rows stay (D4).
- **Auth / permissions / secrets: selected.** `db/roles/node27_write_roles.sql` loses the `jsonb_typeof` ledger allow-list entry (D3). The runner output stays redacted (D4).
- **PostGIS / TimescaleDB domain behavior: selected.** `flood.return_period_result` is a hypertable; dropping it inside the guard transaction must drop its chunks (2.3).
- **Release / operational: selected.** The production migration runs only after an explicit user go-ahead, with backup, receipt and rollback (5.x).
- **Published NHMS artifacts / display identity: selected.** `display_ready_run` and the QHH latest-product selectors read the rebuilt indexes. Their results cannot change (indexes do not change results), and 5.4 records the plan.
- **Documentation / migration notes: selected.** Migration headers; the audit tables in design Context; the production apply receipt.
- **Operator alerting lanes / observer-observed predicate parity: not selected.** No alert predicate reads `run_status`, the six indexes or `flood`. The #2392 file/DB parity is covered under I4 (2.7).
- **Not selected:**
  - Concurrency (a single runner; the `CONCURRENTLY` waits are covered under Release).
  - File IO, Config, Resource limits (`hydro_run` is ~10k rows).

## 1. Baselines

- [x] 1.1 Live drift recorded (design Context).
  - Command: `evidence/catalog_diff.py` (checked in with this change), run on node-27 in `/home/nwm/NWM` (`2024a5e4e`) with `NHMS_RUN_INTEGRATION=1`, `NHMS_INTEGRATION_DATABASE_URL=<nhms replay DSN>`, `PROD_DSNFILE=/home/nwm/tmp/l2/ro.dsn`. Output: `/home/nwm/tmp/m/catalog-diff.json`.
  - The whole difference is `flood`, the six `hydro_run` predicates, and `run_status.frequency_done`.
  - Ledger: 62 rows / 55 files / 7 retired.
  - `hydro_run` status counts; object owners.

## 2. Implementation

- [x] 2.1 **D1 + D2 (#2048):** `db/migrations/000062_hydro_run_status_frequency_done_convergence.sql` and `000063_hydro_run_partial_index_predicate_convergence.sql` as designed, with headers citing #2048 / `b97c16e2`. Tests in `tests/test_migrations.py`:
  - `000063` has, for each of the six indexes, a `DROP INDEX CONCURRENTLY IF EXISTS hydro.<name>` followed by a `CREATE INDEX CONCURRENTLY IF NOT EXISTS <name>`. The CREATE's `ON … (…)` column list and `WHERE` clause are byte-identical, after whitespace normalisation, to the defining migration's (`000021`, `000024`, `000030`, `000031_search_discovery_performance`, `000040`).
  - `000062` adds `frequency_done` `AFTER 'parsed'` with `IF NOT EXISTS`.
- [x] 2.2 **#2048 acceptance 3:** `test_latest_ready_run_discovery_migration_matches_query_predicate_and_order` also asserts that the `WHERE` clause of `000021`'s `hydro_run_latest_ready_run_idx` and of `000063`'s rebuild equals `display_ready_run`'s `h.status IN ('succeeded', 'parsed', 'published')` status list.
- [x] 2.3 **D3 (#2048):** `db/migrations/000064_drop_retired_flood_schema.sql` as one fail-closed `DO` block (`LOCK TABLE … ACCESS EXCLUSIVE` before each count). Real-DB integration test (new file; skips without `NHMS_RUN_INTEGRATION`):
  - **Production-shaped drift.** Build a throwaway database through `000061`, then reproduce production's drift:
    - a `flood` schema with the three tables, `return_period_result` a hypertable, its FK to `hydro.hydro_run` and `run_product_quality`'s `jsonb_typeof` CHECK;
    - the six indexes recreated with production's stale predicates (after `000062`, so `frequency_done` exists);
    - the 7 retired ledger rows.
    - Then run the real `packages.common.migrate.main()` (`DATABASE_URL` = the throwaway). Assert: rc 0; the six `indexdef`s equal a fresh database's; `flood` absent; `run_status` labels in production order; the ledger has `000062`–`000064`.
  - **Populated flood.** The same, with one row in any `flood` table: `000064` fails, all three tables and their row survive, no `000064` ledger row.
  - **Dependent object.** The same, with a view in `public` over a `flood` table: `000064` fails and drops nothing.
  - **INVALID index rerun.** Leave one of the six indexes INVALID (for example by building it `CONCURRENTLY` against a uniqueness conflict, or by marking it invalid with `UPDATE pg_index SET indisvalid = false` as superuser) and delete `000063`'s ledger row. Rerunning the runner gives the clean-run `indexdef`s, all valid.
  - **Fresh database.** `apply_migrations_from_zero` still succeeds, and applying `000062`–`000064` a second time is a no-op.
- [x] 2.4 **D3 sibling:** remove the `pg_catalog.jsonb_typeof` ledger allow-list entry from `db/roles/node27_write_roles.sql` and from `tests/test_node27_write_roles.py` `_LEDGER_ALLOW_LIST`. Replace the non-empty assertion with one that no ledger reason names a retired `flood` migration. The other write-roles tests stay green.
- [x] 2.5 **D4 (#2048):** `packages/common/migrate.py`: `RETIRED_LEDGER_VERSIONS` (7 names, reasons naming `b97c16e2` / #2048) and a pure check function over `(ledger versions, disk file names)` implementing R1–R3, wired into `main()` before the first apply. Tests:
  - unit: each of R1/R2/R3 fails with the offending names listed; the production-shaped ledger (both `000031` rows) passes; an empty ledger passes;
  - real-DB: `main()` against a throwaway database with an extra unrecorded ledger row exits 1, applies nothing (the ledger is unchanged and the next pending file's objects are absent), and its output carries no password.
  - `tests/test_migrate_lock_timeout.py` (the existing `main()` seam): its fake cursor returns ledger rows for the new ledger read instead of falling into the generic branch. Tests assert:
    - the check runs after `configure_migration_session` and `ensure_schema_migrations_table`, and before the first apply;
    - a 55P03 / 57014 raised by the ledger read goes through `_report_migration_failure` as the ledger check, with the lock-holder diagnostics and no ledger write.
- [x] 2.6 **D6 (#2510):** `tests/test_migrations.py`:
  - `EXPECTED_MIGRATIONS` is a literal tuple of the 58 names, with the comment described in D6;
  - the structural test (pattern, strictly increasing, unique);
  - the retired-gap agreement test against `RETIRED_LEDGER_VERSIONS`.
  - No other test computes its expectation from the glob it checks.
- [x] 2.7 **D7 (#2392):** `packages/common/state_manager.py` `get_earliest_clone_row_for_model_source` predicate and trim-set constant; both docstrings rewritten per D7. Tests:
  - `tests/test_real_database_integration.py` (real PostgreSQL), next to the existing earliest-clone-row test (`:671`):
    - an earliest row with a whitespace-only parent, one per shape: `'   '`, `'\t'`, `'\n'`, `'\u3000'`;
    - an earliest row whose parent is the padded self `' <model_id> '`;
    - each followed by a later legitimate row.
    - The reader returns the later row, and the resolved `LineageCutover` equals the file plane's for the same rows (I4).
  - The trim-set constant is a literal; a unit test asserts it equals the computed `str.isspace()` set.
  - One real-DB assertion that `btrim(x, <constant>)` equals Python's `x.strip()` for a string containing every `isspace()` character on both ends, and that `current_setting('server_encoding') = 'UTF8'`.
  - The docstrings at `state_manager.py` ~:876 (publisher reader) and `scheduler_lineage.py` ~:246 no longer describe the divergence.
  - `tests/test_scheduler_lineage.py`: the SQL-shape test (`:738`) pins both new conjuncts and the parameter.
- [x] 2.8 CI routing: `scripts/select_ci_tests.py` routes `db/migrations/**`, `packages/common/migrate.py` and `db/roles/**` to the new integration test file, with a `tests/test_select_ci_tests.py` row. `real-db-integration`'s `database` filter already covers `db/**`.
- [x] 2.9 Text made false by `000062` (design Sibling surfaces) now says "a convergence-only ledger member since `000062`, never written": the 6 doc lines, the `apps/api/response_models/forecast.py:33` comment, and the `tests/test_hydro_run_public_projection.py:256-257` comment. Behaviour does not change (`HydroRun.status` stays `str`; OpenAPI `RunStatus` unchanged).

## 3. Verification

- [x] 3.1 Local: `uv run ruff check .`; `uv run pytest -q tests/test_migrations.py tests/test_migrate_lock_timeout.py tests/test_scheduler_lineage.py tests/test_node27_write_roles.py tests/test_hydro_status_set_parity.py tests/test_hydro_run_public_projection.py tests/test_select_ci_tests.py <new unit tests>`; `openspec validate schema-ledger-convergence-and-clone-parent-normalisation --strict --no-interactive`. Plus the #2510 reverse checks from D6 (temporary edits, not committed), with output.
  - **Result (2026-09-25):**
    - ruff clean; `openspec validate --strict` valid.
    - Targeted pytest: 191 passed / 30 skipped at `362fdb951` (integration skipped locally); after fix pass 1, 129 passed / 9 skipped on the fix-delta files. The implementer's `select_ci_tests` run: 895 passed.
    - #2510 reverse checks (temporary edits, reverted): renaming, removing, duplicating a prefix, or adding an unpadded name each fails the new inventory tests, while the old test passes the rename. PR body.
    - Red proofs: the D4 tests fail on master's `migrate.py` (10), the D7 SQL-shape test fails on the old SQL, the allow-list test fails on master's roles SQL, a perturbed `000063` predicate fails 2 tests, and `frequency_done` added to a status set fails the F3 test.
- [x] 3.2 node-27 read-only: the D4 check function run against production's `schema_migrations` (`nhms_display_ro`) passes with exactly the 7 retired versions.
  - **Result (node-27, 2026-09-25, `362fdb951`):** production ledger 62 rows, 58 files, `ledger_disk_mismatches == []`, pending exactly `000062`–`000064`, 7 retired recognised. `/home/nwm/tmp/m/ledger-check-pre.txt`.
- [ ] 3.3 node-27 disposable-DB pytest on the frozen SHA: the new integration file, `tests/test_real_database_integration.py`, `tests/test_migrations.py`, `tests/test_scheduler_lineage.py`, `tests/test_node27_write_roles.py`, `tests/test_node27_1729_evidence_basin_delete_integration.py`. Then the node-27 full pytest; its failure set must equal master's (#2615 only).

## 4. Review / CI

- [ ] 4.1 Review rounds recorded with fix_gate. CI green, including SQL Migration Dry Run on the non-draft PR.

## 5. After merge

- **Standing rule from merge until 5.4 is done:** any `packages.common.migrate` run on node-27 applies `000062`–`000064`. So no migration may run on node-27 before 5.1 is captured and the go-ahead (5.2) is given. A later PR that brings its own migration waits for this window or joins it.
- **Pinned pre-drop audit source (design D3).** While `flood` exists, the only audit source that can pass is master's roles SQL at `e340dbc10`, which still trusts `jsonb_typeof`. That holds wherever node-27's checkout is. Every pre-write audit in this procedure uses it:

  ```bash
  git -C /home/nwm/NWM show e340dbc10:db/roles/node27_write_roles.sql \
    | docker exec -i nhms-db psql -U nhms -d nhms -X -v ON_ERROR_STOP=1 \
        -v do_roles=off -v do_ownership=off -v do_audit=on -v strict_audit=on
  ```

  Pre-merge rehearsal (2026-09-25, read-only against production): the pinned source gives rc 0 and `0 untrusted`. The branch's SQL gives rc 3 with exactly one untrusted function, `pg_catalog.jsonb_typeof`, from the two `flood.run_product_quality` CHECKs. `/home/nwm/tmp/m/audit-{old,new}.txt`.

- [ ] 5.1 Pre-apply capture on node-27, read-only:
  - the `flood` row counts (must be 0 / 0 / 0) and `pg_depend` entries on `'flood'::regnamespace`;
  - TimescaleDB state of `flood.return_period_result`: jobs, compression settings, chunks and continuous aggregates. A pre-merge read (2026-09-25) found 0 of each;
  - the six `indexdef`s and `indisvalid`;
  - the `run_status` labels;
  - the `schema_migrations` count;
  - `pg_dump -Fc -n flood` to `/home/nwm/tmp/m/flood-pre-000064.dump`, verified with `pg_restore --list`.
- [ ] 5.2 **Stop and ask the user for the production go-ahead.** Present 5.1, the window steps (5.3) and the rollback (D3). Nothing below runs without a yes.
- [ ] 5.3 The window, in order, recording each step's time and output (precedent: `docs/runbooks/receipts/2026-09-12-issue-2145-geometry-generation-migration-node27.md`):
  1. stop `nhms-node27-autopipe.timer` and `nhms-node27-download.timer`, and wait for in-flight runs to drain;
  2. **audit-only from the pinned source** (above). It must be clean, otherwise abort and restart the timers;
  3. `git status --porcelain` empty, then `git pull --ff-only`;
  4. `.venv/bin/python -m packages.common.migrate` as `nhms`, every attempt recorded.
     - A `lock_timeout` retry is safe: `000063` replays all six rebuilds (D2).
     - `000064`'s `DROP TABLE`s also take ACCESS EXCLUSIVE briefly on `hydro.hydro_run` and `core.model_instance`, because dropping an FK drops the referenced table's RI triggers. Display readers can queue behind it for up to `lock_timeout`; on a 55P03, `000064` fails atomically and is safe to rerun.
     - **Retries inside the window** need no new audit. The timers stay stopped, so no write-role writer runs between attempts, and step 2's audit covers the whole window.
  5. `bash scripts/node27_provision_write_roles.sh`: rc 0 and a clean strict audit (the new SQL, with `flood` gone);
  6. restart both timers and confirm they are `active`.
  - **Abort in the window** (the operator gives up after step 3 with `flood` still present): restart the timers and record the state. The checkout is post-merge, and some of `000062`/`000063` may be applied. Until `000064` has run, every pre-write audit on node-27, for any purpose, uses the pinned source. The next window starts again at step 1.
  - **Rollback of `000064`** (D3): `pg_restore` of the dump, plus deleting the `000064` ledger row. It brings the CHECKs back, so from then on the pinned source is again the only passing audit source, and the post-provision strict audit (step 5) would be red until `000064` is reapplied. Record both.
- [ ] 5.4 Post-apply receipt:
  - the catalog diff (`evidence/catalog_diff.py` at the merged SHA, same command as 1.1) is empty in every category;
  - the D4 check passes read-only;
  - the six `indexdef`s are all valid;
  - `EXPLAIN` of `display_ready_run`'s statement records the plan chosen;
  - display API `/health` is 200 and there are no new 500s.
  - Recorded in the archive PR, with the SHA of the probe used.

## Evidence Floor

1. `git diff --stat e340dbc10 -- db/migrations/` lists only the three added files (I1).
2. On node-27 production, after the 5.3 window, the six `hydro_run` partial `indexdef`s equal the ledger's and the full catalog diff against a fresh build is empty (5.4). The roles audit is clean before and after the write (5.3). #2048 acceptance 1 and 7 (the other enums) are closed by this receipt.
3. `000064` refuses a populated or depended-on `flood` and drops nothing; a failed `000063` rebuild is safe to rerun (2.3, node-27 3.3).
4. The runner refuses an unrecorded ledger/disk mismatch before applying anything, and passes production's ledger (2.5, 3.2).
5. The inventory test fails on a rename, removal, duplicate or unpadded prefix (2.6; reverse checks in the PR body).
6. The DB plane returns the later legitimate clone row for all four whitespace shapes and the padded self-reference, and equals the file plane (2.7, node-27 3.3).
7. #2048's `run_status` decision (D1), the 13-file audit (design Context) and the CI schema-diff decision (D5) are written down.
