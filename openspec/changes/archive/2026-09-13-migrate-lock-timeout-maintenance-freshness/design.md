# Design

## Risk triage

```text
Issue type: bugfix (#2277) + ops observability (#1769/#1770)
Project profile: NHMS (openspec/project-profile.md)
Blast radius: high — migrate.py is the shared entry for CI dry run, node-27 bring-up,
  qhh cycle scripts and the imminent I8 live DDL window; governance alert path pages ops
Fixture level: expanded   Repair intensity: high
Upstream suggested level: absent (hand-written issues)
Why: shared entrypoint/CLI; migration; production alert (critical → OnFailure) semantics;
  persisted evidence (receipt fields)
OpenSpec change: migrate-lock-timeout-maintenance-freshness (generated)
```

## Decisions

1. **Guard lives in `main()` only** (`configure_migration_session(connection)`), applied
   after connect and before `ensure_schema_migrations_table`. Library helpers keep
   signatures and never touch caller sessions (`tests/integration_helpers.py`,
   `scripts/apply_smoke_migrations.py`, `tests/test_real_database_integration.py`).
2. **Precedence** `NHMS_MIGRATE_LOCK_TIMEOUT` > non-zero session value (PGOPTIONS / role /
   db) > `5s`. Rationale: runbook §4.10.2 already passes `PGOPTIONS lock_timeout=5s
   statement_timeout=120s`; an operator raising it for a specific window must not be
   silently overridden. Values set with `SELECT set_config(name, %s, false)` (bound
   parameter, PostgreSQL validates units); `0` is accepted only as an explicit env value.
3. **No default `statement_timeout`**: would kill backfills / index builds (#2277 body).
4. **No retry, no cancel** — one command = one terminal state; ledger idempotency makes
   operator retry free. Diagnostic lists candidate holders; it cannot name the exact
   relation (PostgreSQL's `55P03` message carries none) so it lists non-idle sessions by
   transaction age, which is what the #2145 operator needed.
5. **CIC footgun documented, not engineered**: `CREATE INDEX CONCURRENTLY` waits on old
   virtual xids through the lock manager, so a lock timeout can leave an INVALID index that
   `IF NOT EXISTS` skips on retry. All CIC migrations (000030–000054) are already applied in
   production, but fresh bring-up/rebuilds still apply them under the default; only 000052
   self-heals its INVALID index, so the runbook note requires the INVALID-index check before
   retrying any CONCURRENTLY file that hit a lock timeout.
6. **Autovacuum mechanism is closed, not re-fixed**: crash-restart counter discard, fixed by
   container stop signal/timeout (08-23) and still configured (09-13 probe). This change adds
   no container/DB configuration change and no migration.
7. **Check location = governance channel** (issue #1769 recommended option): the audit
   already collects autovacuum settings; #1765 (closed) delivered critical → exit 1 →
   `OnFailure=` alert, so the signal reaches a human. The autopipe stats guard is not widened
   (would mask autovacuum failure by doing its work — #1769 "备选" tradeoff).
8. **Severity**: per-table stale output is a warning (a single hot table may lag); critical
   only when stale tables coexist with database-wide silence > 24h of the *same* output kind
   (analyze-stale ↔ `max(last_autoanalyze)`, vacuum-stale ↔ `max(last_autovacuum)`) — the 08-20
   shape, and it does not mask an autoanalyze-only silence. An unavailable probe is a warning
   (`MAINTENANCE_OUTPUT_UNAVAILABLE`), never green, never paging.
   Multiplier 10 and 24h match the #1769 example criterion and tolerate normal naptime and
   long single-table vacuums.
9. **`core.basin` / `core.mesh_version` disposition = included in the check as info,
   no per-table reloptions**: 18 live rows each, 19 modifications below the default analyze
   threshold (50); planner misestimates on an 18-row table are immaterial; once churn passes
   50 autoanalyze leaves the class, and failure to do so is caught by the stale rule. Adding
   reloptions needs a migration, which would break I8's "pending set is exactly 000059" gate.
10. **Known blind spot (documented)**: a crash that discards counters makes every
    counter-based check read green; `stats_reset` stays NULL. Prevention is the container
    stop contract; planner stats survive in `pg_statistic` and the authority bootstrap leg
    covers double-NULL tables.

## Invariant Matrix

- Governing invariant: (A) no statement executed by `packages.common.migrate` waits
  unboundedly for a lock unless an operator explicitly asked for it, and a lock failure never
  records the ledger row; (B) a sustained absence of autovacuum/autoanalyze output on tables
  whose own counters demand it is surfaced as a non-green governance finding, and a
  database-wide silence reaches the alert path.
- Source-of-truth identity/contract: session GUCs `lock_timeout`/`statement_timeout`;
  `public.schema_migrations.version`; `pg_stat_all_tables` counters + `pg_class.reloptions`
  + `pg_settings` autovacuum thresholds; receipt `recommendations[].code/severity`.
- Producers: `migrate.main`, `configure_migration_session` (new); `collect_postgres`
  `maintenance_output` query; `_recommendations` in `node27_resource_governance.py`.
- Validators/preflight: env value validation via `set_config`; summary/row shape checks in
  the evaluator (missing/non-numeric fields → no recommendation, never crash).
- Storage/cache/query: `public.schema_migrations` ledger write in `record_migration` (only
  after all statements succeed — unchanged); read-only catalog/stat queries.
- Public routes/entrypoints: `python -m packages.common.migrate`, `make migrate`,
  `scripts/run_qhh_cycle.sh:425`, `scripts/run_qhh_backend_smoke.sh:157`, CI "SQL Migration
  Dry Run"; `node27_resource_governance_once.sh` / systemd unit.
- Frontend/downstream consumers: none - no API/UI surface; the `--summary` file and
  `OnFailure=` alert body consume recommendation codes.
- Failure paths/rollback/stale state: `55P03`/`57014` exit 1 with ledger untouched;
  diagnostic-query failure; invalid env value; `maintenance_output` query error isolated;
  stats wiped (documented blind spot).
- Evidence/audit/readiness: receipt JSON; `evidence/` probe + isolated-checkout live audit;
  `collect_postgres` sole consumer is resource governance (receipt, `--summary`, `OnFailure=`
  body); the cold governance schema does not read `postgres`.
- Regression rows:
  - migrate default session (lock 0, env unset) -> `5s` applied, statement_timeout untouched
  - migrate with PGOPTIONS `lock_timeout=30s` -> kept
  - migrate env `NHMS_MIGRATE_LOCK_TIMEOUT=200ms` + real ACCESS EXCLUSIVE holder -> exit 1,
    holder pid printed, ledger row absent (real DB)
  - invalid env value -> exit 1 before any statement, variable named
  - diagnostic query raising -> original error + exit 1 preserved
  - `apply_migration` on caller connection -> caller GUCs unchanged
  - guarded `main()` over full history on empty isolated DB (node-27; CI calls only
    `apply_migration`) -> exit 0, ledger complete, 0 INVALID indexes
  - holder in `idle in transaction (aborted)` -> listed
  - DSN password / `PASSWORD '...'` query head -> redacted in output
  - `maintenance_output` error/missing -> `MAINTENANCE_OUTPUT_UNAVAILABLE` warning, exit 0
  - analyze-only silence with live autovacuum -> critical
  - #1769 river_segment row (36.4×, last_autoanalyze null) -> `TABLE_STATISTICS_STALE`
  - freshly autoanalyzed row -> no finding
  - manually analyzed once, now 20× over -> `TABLE_STATISTICS_STALE`
  - 08-20 shape (met_station 65.9× dead, output maxima >24h) -> critical, exit 1, stderr
    prefix, receipt status `completed`
  - stale warning but output maxima fresh -> warning only, exit 0
  - `core.basin` zero-stat -> info only
  - `autovacuum_enabled=false` chunk -> excluded (real DB)
  - existing `DEAD_TUPLE_HOTSPOT`, working-set, filesystem recommendations -> unchanged

## Boundary-surface checklist

- Shared helper roots: `packages/common/migrate.py`, `packages/common/node27_cold_governance_collection.py`
  (`collect_postgres` is called only by resource governance).
- Public entrypoints: migrate CLI and its three script/Makefile callers; governance CLI/unit.
- Read surfaces: catalog/stat queries only.
- Write surfaces: ledger insert unchanged; no new writes.
- Unchanged downstream consumers: CI dry run, integration helpers, smoke migration runner,
  autopipe stats guard (not modified), resource-governance `--summary`/`OnFailure=` consumers.

## Risk packs

- Public API / CLI / script entry: selected — migrate CLI output/exit codes; governance exit code.
- Config / project setup: selected — two new env vars with precedence; no new required config.
- File IO / path safety / overwrite: not selected — no file paths added.
- Schema / columns / units / field names: selected — new receipt section/codes; GUC unit strings.
- Auth / permissions / secrets: selected — diagnostic prints other sessions' query heads that may
  carry `PASSWORD '...'` literals (§9.6 role provisioning in I8) into `migrate.log`/receipts: redact via
  `redact_database_dsn` + SQL password-literal rule, unit-tested; DSN never printed; non-superuser
  roles see `<insufficient privilege>` query text (acceptable).
- Concurrency / shared state / ordering: selected — lock queue behavior on a live primary.
- Resource limits / large input / discovery: selected — probe bounded (LIMIT 50, 20s statement_timeout
  already set by collector); diagnostic bounded to 20 rows / 120 chars.
- Legacy compatibility / examples: selected — library callers and runbook PGOPTIONS usage unchanged.
- Error handling / rollback / partial outputs: selected — ledger not recorded on failure;
  partial-file autocommit disclosure; probe error isolation.
- Release / packaging / dependency compatibility: not selected — no dependency change.
- Documentation / migration notes: selected — runbook §4.10.2 + container ops runbook.
- Domain PostGIS/TimescaleDB behavior: selected — exclude compressed chunk phantom counters.
- Other domain packs (geospatial, hydro-met windows, SHUD numerical): not selected — untouched.

## Seams under test

- `packages.common.migrate.main()` with a fake connection/cursor (unit) and against a real
  PostgreSQL (integration marker, node-27 isolated DB / CI real-db job).
- `configure_migration_session(connection)` public helper.
- `_recommendations(receipt, thresholds)` evaluator over receipt dicts (unit) and `main()` exit code.
- `collect_postgres(url)` against a real database (integration marker).

## Non-goals

- VACUUM FULL / repack of `met.met_station` (325 MB for 42k rows on 2026-09-13) — operator
  window, follow-up issue for churn/fillfactor (`station_set_flip` HOT ratio 9%).
- Any migration, reloptions change, container/PG config change, restart.
- Deploying to the production unit (node-27 production checkout moves only in the I8 window).
- Changing autopipe stats guard legs (#1378/#1643/#1468 semantics).
- Retry loop or automatic lock-holder cancellation in migrate.
- Detecting crash-discarded counters from SQL.
