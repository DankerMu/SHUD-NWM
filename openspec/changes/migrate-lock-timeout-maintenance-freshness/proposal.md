# Migration runner lock safety + autovacuum output-freshness check

## Why

- **#2277**: `packages/common/migrate.py` connects with no `lock_timeout`. On the live
  node-27 primary a pending `AccessExclusiveLock` DDL waits forever and, because the
  PostgreSQL lock queue is FIFO, blocks every later reader of the same relation. The
  #2145 window (2026-09-12) only survived because the operator added
  `PGOPTIONS="-c lock_timeout=5s ..."` by hand. Epic #1979 I8 (#1987) is about to apply
  `000059` to the live database; the runner must carry the guard itself before that.
- **#1769 / #1770**: autovacuum/autoanalyze output on node-27 went silent from
  2026-08-20 while every configuration surface looked healthy, and nothing checked the
  *output*. The mechanism was established on 2026-08-23 (#1770 comments): `docker restart`
  → PostgreSQL smart shutdown never completes against long-lived clients → Docker SIGKILL
  → crash recovery → PG15 discards cumulative statistics → autovacuum's trigger inputs
  (`n_dead_tup`, `n_mod_since_analyze`) read zero. The container was rebuilt with
  `StopSignal=SIGINT`, `StopTimeout=300`, `ShmSize=1G`. The 2026-09-13 read-only probe
  (`evidence/2026-09-13-node27-autovacuum-output-probe.txt`) shows the fix held across the
  2026-09-12 container recreation (clean shutdown logged, no crash recovery since 08-24) and
  output is live: user-table `last_autovacuum` 06:37Z / `last_autoanalyze` 06:51Z, zero
  tables over either threshold. What is still undelivered is the **output-level check**, so
  the next silence of any cause is visible.

## What Changes

- `migrate.py` `main()` configures the migration session before the first statement:
  `lock_timeout` defaults to `5s` unless an explicit `NHMS_MIGRATE_LOCK_TIMEOUT` or a
  non-zero session value (e.g. from `PGOPTIONS`) exists; `statement_timeout` is set only
  when `NHMS_MIGRATE_STATEMENT_TIMEOUT` is given. Effective values are printed.
- On `lock_not_available` (SQLSTATE `55P03`) the runner prints the other sessions that
  may hold the lock (pid, usename, application_name, state, wait_event_type, xact age,
  query head) before exiting 1; no retry loop, no `pg_cancel_backend`.
- `collect_postgres` gains a bounded `maintenance_output` probe (reloptions-aware effective
  vacuum/analyze thresholds, last auto/manual ages, zero-statistics class, user-table output
  maxima). `node27_resource_governance.py` evaluates it into `TABLE_STATISTICS_STALE`,
  `TABLE_VACUUM_DEBT_STALE` (warning), `AUTOVACUUM_OUTPUT_STALLED` (critical → exit 1 →
  `OnFailure=` alert, delivered by #1765) and `TABLE_ZERO_STATISTICS` (info).
- Runbook notes: migrate session guard precedence and the CIC/INVALID-index footgun; the
  governance codes and the counter-wipe blind spot.
- Diagnosis receipt for #1769/#1770 in `evidence/`.

No database migration is added (I8 requires the pending set to be exactly `000059`).
No production write, VACUUM, ANALYZE or restart is performed.

## Impact

- Code: `packages/common/migrate.py`, `packages/common/node27_cold_governance_collection.py`,
  `scripts/node27_resource_governance.py`.
- Tests: `tests/test_migrations.py` (or a new focused file), real-DB integration test,
  `tests/test_node27_resource_governance.py`.
- Docs: `docs/runbooks/tier-node27-timeseries-storage.md` §4.10.2 migrate note,
  `docs/runbooks/node-27-database-container-operations.md` (maintenance output check).
- Callers unchanged: `Makefile`, `scripts/run_qhh_cycle.sh`, `scripts/run_qhh_backend_smoke.sh`
  call `main()` and gain the guard; `tests/integration_helpers.py`,
  `scripts/apply_smoke_migrations.py` import `apply_migration` / `ensure_schema_migrations_table`
  with their own connections and keep today's behavior.
