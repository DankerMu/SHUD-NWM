# Tasks

Issues: #2277 (closes), #1769 (closes on live isolated-checkout receipt), #1770 (refs — mechanism
closed here; met_station reclaim needs an operator window and a follow-up issue).
Fixture level: expanded; repair intensity: high (see design.md Invariant Matrix).

## 1. Migration runner lock safety (#2277)

- [x] 1.1 `configure_migration_session(connection)` in `packages/common/migrate.py`; called from
  `main()` after connect, before `ensure_schema_migrations_table`; precedence env > non-zero session
  > `5s`; `statement_timeout` only from env; `set_config(..., false)` with bound parameter; prints
  effective values; invalid env → exit 1 naming the variable, before any statement.
  Verify: unit tests with fake connection/cursor — default (session `0`, env unset → `5s`, no
  statement_timeout call), PGOPTIONS-style session `30s`/`120s` kept, env `200ms`/`10min` applied,
  explicit env `0` applied, invalid value → `SystemExit(1)` and no `CREATE TABLE`/ledger SQL issued.
- [x] 1.2 Lock-failure diagnostics in `main()`: `55P03` → filename, error, effective lock_timeout,
  ≤20 non-self sessions in current database with `xact_start IS NOT NULL` ordered by xact_start
  (pid, usename, application_name, state, wait_event_type, xact age, query head = redact full text
  first, then truncate to 120 chars; text through `redact_database_dsn` + SQL `PASSWORD '<literal>'` redaction),
  autocommit partial-file disclosure, exit 1, no ledger row; `57014` → names effective
  statement_timeout; diagnostic query failure → "diagnostic unavailable" + original error + exit 1.
  Never print `DATABASE_URL`. Effective values printed with `pg_settings.source`.
  Verify: unit tests for `55P03` (holder rows printed, including an `idle in transaction (aborted)`
  holder), `57014`, diagnostic raising, secrets (DSN password `s3cretpw` + holder query
  `ALTER ROLE x PASSWORD 'secret'` → neither string in stdout; a `PASSWORD 'longsecretvalue'` literal
  spanning char 120 → no >3-char substring of it in stdout); real-DB test
  (`integration` marker): session A `BEGIN; LOCK TABLE <scratch> IN ACCESS EXCLUSIVE MODE`, runner
  `main()` over a temp migrations dir with one `ALTER TABLE <scratch> ADD COLUMN ...` file and
  `NHMS_MIGRATE_LOCK_TIMEOUT=200ms` → exit 1 in < 10 s, output contains A's pid and
  application_name, `public.schema_migrations` has no row for the file; after A rolls back the
  same run exits 0 and records it.
- [x] 1.3 Library callers unchanged: real-DB or unit assertion that `apply_migration` leaves a
  caller connection's `lock_timeout` unchanged; existing `tests/test_migrations.py`,
  `tests/test_real_database_integration.py` pass untouched.
  Verify (orchestrator, node-27 isolated scratch DB — CI never calls `main()`): empty database,
  connecting as superuser owner, `nhms_ingest_rw` created first by only the `DO $roles$` block of `db/roles/node27_write_roles.sql`
  (§9.6; 000059 `OWNER TO` needs it), timeout env unset, `python -m packages.common.migrate` → exit 0, ledger rows == number of
  `db/migrations/*.sql`, `SELECT count(*) FROM pg_index WHERE NOT indisvalid` == 0; plus
  `PGOPTIONS='-c lock_timeout=30s'` run prints `30s` kept.
- [x] 1.4 Runbook: `docs/runbooks/tier-node27-timeseries-storage.md` §4.10.2 — runner now defaults
  `lock_timeout=5s`; precedence (env > PGOPTIONS > default); `statement_timeout` still only via
  PGOPTIONS/env; the CIC INVALID-index footgun (fresh bring-up/rebuild still applies the
  CONCURRENTLY files 000030–000054 under the default; only 000052 self-heals its INVALID index —
  after any lock-timeout failure on a CONCURRENTLY file run the INVALID-index check before retry);
  the new effective-settings output lines that I8 receipt captures will contain. Do not alter the window's command sequence.

## 2. Maintenance output freshness check (#1769 / #1770 check side)

- [x] 2.1 `collect_postgres` adds `maintenance_output = {status, summary, rows}` per spec: reloptions
  override else `current_setting` thresholds; excludes `pg_catalog`/`information_schema`/`pg_toast`
  and `autovacuum_enabled=false`; negative `reltuples` → 0; ages in seconds; bounded to over-threshold
  or zero-stat rows, LIMIT 50; errors isolated to `status: "error"` without breaking other sections.
  Sole consumer is `scripts/node27_resource_governance.py` (receipt, `--summary`, `OnFailure=` body);
  the cold governance schema does not read the `postgres` section.
  Verify: real-DB test (`integration`): create scratch table, set `autovacuum_enabled=false` on one
  (exclusion target) and custom analyze reloptions on another, generate modifications, flush stats
  (`pg_stat_force_next_flush()`), probe immediately (an autovacuum race may only remove a row, so the
  assertion on the reloptions table tolerates absence by retrying generation once) → the disabled one absent, the reloptions one carries the overridden threshold, a
  never-analyzed empty-page table with rows appears as zero-stat; row
  shape holds (column names/types as the evaluator expects).
- [x] 2.2 `_recommendations` derives `TABLE_STATISTICS_STALE`, `TABLE_VACUUM_DEBT_STALE` (warning),
  `AUTOVACUUM_OUTPUT_STALLED` (critical, analyze-stale↔analyze maximum, vacuum-stale↔vacuum maximum),
  `MAINTENANCE_OUTPUT_UNAVAILABLE` (warning), `TABLE_ZERO_STATISTICS` (info); thresholds on
  `AuditThresholds` (`maintenance_stale_multiplier=10`, `maintenance_stale_age_seconds=86400`);
  malformed row fields → skip row, never raise; section missing/malformed/`status != ok` (with
  `postgres.status == ok`) → `MAINTENANCE_OUTPUT_UNAVAILABLE` warning.
  Verify (unit, `tests/test_node27_resource_governance.py`), each with explicit rows:
  - #1769 river_segment (thr 500/0.01, reltuples 209126, mods 94380, autoanalyze null, analyze 3.5 d)
    → one warning, ratio ≈ 36.4
  - same row mods 0, autoanalyze 10 min → no finding
  - manual analyze 5 d, autoanalyze null, mods 20× → warning
  - 08-20 shape: met_station dead 65.9×, last_autovacuum 67 h, summary maxima 61 h/67 h → critical;
    `main()` returns 1, stderr `RESOURCE_GOVERNANCE_CRITICAL:AUTOVACUUM_OUTPUT_STALLED`, receipt
    `status == "completed"`
  - stale warning with summary maxima 10 min → warning only, `main()` returns 0
  - analyze-stale row, analyze maximum 61 h, vacuum maximum 10 min → critical
  - core.basin zero-stat (relpages 0, reltuples -1, live 18, mods 19) → info only
  - `maintenance_output.status == "error"` → exactly `MAINTENANCE_OUTPUT_UNAVAILABLE` warning,
    existing codes unchanged, `main()` returns 0; `postgres.status != ok` → no maintenance findings
  - existing recommendation tests pass unchanged.
- [x] 2.3 Runbook `docs/runbooks/node-27-database-container-operations.md`: the five codes and their
  meaning; stop contract (SIGINT/300) is what keeps counters alive; `stats_reset IS NULL` is not
  evidence of no reset; counter-wipe blind spot; `core.basin`/`core.mesh_version` disposition.

## 3. Diagnosis + live evidence (orchestrator, node-27)

- [x] 3.1 Read-only production probe captured: `evidence/2026-09-13-node27-autovacuum-output-probe.txt`
  (output live, 0 over-threshold, clean shutdown since 08-24, container stop contract present,
  000059 pending).
- [x] 3.2 Diagnosis receipt `evidence/issue-1769-1770-diagnosis.md`: mechanism conclusion (crash
  restart counter discard) with the SIGTERM burst source stated: `docker restart`/container stop →
  SIGTERM → smart shutdown emits `terminating ... due to administrator command` for autovacuum/
  background workers → clients never disconnect → Docker 10 s SIGKILL → crash recovery → PG15 stats
  discard (plus the integration-test `DROP DATABASE ... WITH FORCE` source noted 08-23), exclusion list, today's state, met_station regrowth (325 MB / 42,029 rows,
  HOT 9%), #1765 relation (delivered: critical → exit 1 + `OnFailure=`), basin/mesh_version
  disposition, deviation "live verdict green".
- [x] 3.3 node-27 isolated checkout at PR head: focused pytest (unit + `integration` tests against an
  isolated scratch database, never production `nhms`), and a read-only governance audit against
  production with the new code → receipt shows `maintenance_output.status == "ok"` and the maintenance
  verdict recorded in `evidence/`.
- [ ] 3.4 Rewrite the mechanism sections of #1769 and #1770 from "候选/未定" to "已定位" (issue comment
  or body edit linking the receipt), per #1770 08-23 closing condition 2.
- [x] 3.5 Follow-up issue for met_station churn/bloat regrowth (issue-scribe) → #2300.

## Evidence floor

- `uv run ruff check .`
- `uv run pytest -q tests/test_migrations.py tests/test_node27_resource_governance.py` (+ new focused files)
- node-27: `integration`-marked new tests on isolated DB; isolated-checkout read-only live audit
- `openspec validate migrate-lock-timeout-maintenance-freshness --strict --no-interactive`
