# database-maintenance-output-freshness Specification

## Purpose
Detect autovacuum/autoanalyze output stalls on node-27 by what the maintenance workers actually produced (per-table staleness and database-wide silence), not by configuration, and route critical stalls through the resource governance exit-code / OnFailure alert channel (#1769, #1770).

## Requirements
### Requirement: Resource governance MUST judge autovacuum and autoanalyze by output, not configuration

`collect_postgres` MUST collect a `maintenance_output` section with (a) per-relation rows for ordinary tables and materialized views outside `pg_catalog`, `information_schema` and `pg_toast`, excluding relations whose `reloptions` contain `autovacuum_enabled=false`, carrying `schema`, `relation`, `relpages`, `reltuples`, `n_live_tup`, `n_dead_tup`, `n_mod_since_analyze`, the effective vacuum threshold and effective analyze threshold (per-table `reloptions` `autovacuum_[vacuum|analyze]_threshold` / `_scale_factor` override, else the cluster settings, with negative `reltuples` treated as 0), and the ages in seconds of `last_autovacuum`, `last_vacuum`, `last_autoanalyze`, `last_analyze` (null when never); the row set MUST be bounded to relations over either effective threshold or in the zero-statistics class, at most 50 rows ordered by the larger over-threshold ratio; and (b) one summary with the user-relation `max(last_autovacuum)` and `max(last_autoanalyze)` ages and the counts of relations over each threshold. A query failure MUST be recorded as `maintenance_output.status = "error"` (with the exception class name) without failing the rest of the receipt. `node27_resource_governance.py` MUST derive:

- `TABLE_STATISTICS_STALE` (warning) when `n_mod_since_analyze > stale_multiplier × analyze_threshold` and the newer of `last_autoanalyze`/`last_analyze` is null or older than `stale_age_seconds`;
- `TABLE_VACUUM_DEBT_STALE` (warning) when `n_dead_tup > stale_multiplier × vacuum_threshold` and the newer of `last_autovacuum`/`last_vacuum` is null or older than `stale_age_seconds`;
- `AUTOVACUUM_OUTPUT_STALLED` (critical) when (`TABLE_STATISTICS_STALE` fires for at least one relation and the user-relation `max(last_autoanalyze)` is null or older than `stale_age_seconds`) or (`TABLE_VACUUM_DEBT_STALE` fires for at least one relation and the user-relation `max(last_autovacuum)` is null or older than `stale_age_seconds`), so an autoanalyze-only silence is not masked by live autovacuum;
- `MAINTENANCE_OUTPUT_UNAVAILABLE` (warning) when `postgres.status == "ok"` but `maintenance_output` is missing, malformed, or its `status` is not `ok` — an unobservable output is never reported as healthy;
- `TABLE_ZERO_STATISTICS` (info) when `relpages = 0`, `reltuples < 0`, `n_live_tup > 0` and both analyze timestamps are null;

with defaults `stale_multiplier = 10` and `stale_age_seconds = 86400`. A manual `ANALYZE`/`VACUUM` alone MUST NOT hide a relation whose output is stale again later (the judgement uses modification counts and ages, not the double-NULL bootstrap signature). `pg_stat_database.stats_reset` MUST NOT be used as evidence that counters were never reset. The existing critical → exit 1 → `OnFailure=` path MUST carry `AUTOVACUUM_OUTPUT_STALLED` without receipt `status` changing from `completed`.

#### Scenario: #1769 historical row is judged stale
- **GIVEN** a `core.river_segment` row with `reloptions` threshold 500 / scale 0.01, `reltuples = 209126`, `n_mod_since_analyze = 94380`, `last_autoanalyze = null`, `last_analyze` 3.5 days old
- **WHEN** recommendations are built
- **THEN** a `TABLE_STATISTICS_STALE` warning names `core.river_segment` with ratio ≈ 36.4

#### Scenario: Freshly autoanalyzed table does not alert
- **GIVEN** the same row with `n_mod_since_analyze = 0` and `last_autoanalyze` 10 minutes old
- **WHEN** recommendations are built
- **THEN** no maintenance-output recommendation is emitted for it

#### Scenario: Manually analyzed table leaves the bootstrap set but not this check
- **GIVEN** a table with `last_analyze` 5 days old, `last_autoanalyze` null, and modifications 20× its effective analyze threshold
- **WHEN** recommendations are built
- **THEN** `TABLE_STATISTICS_STALE` fires for it

#### Scenario: Database-wide silence is critical and exits non-zero
- **GIVEN** `met.met_station` with `n_dead_tup` 65.9× its vacuum threshold, `last_autovacuum` 67 hours old, and user-relation output maxima 61 and 67 hours old
- **WHEN** the audit runs
- **THEN** `AUTOVACUUM_OUTPUT_STALLED` is critical, stderr carries `RESOURCE_GOVERNANCE_CRITICAL:AUTOVACUUM_OUTPUT_STALLED`, the receipt status is `completed`, and `main()` returns 1

#### Scenario: Autoanalyze-only silence is critical even while autovacuum runs
- **GIVEN** a relation with `TABLE_STATISTICS_STALE`, user-relation `max(last_autoanalyze)` 61 hours old and `max(last_autovacuum)` 10 minutes old
- **WHEN** recommendations are built
- **THEN** `AUTOVACUUM_OUTPUT_STALLED` is critical

#### Scenario: Unobservable output is a warning, not green
- **GIVEN** `postgres.status == "ok"` and `maintenance_output = {"status": "error", "error": "QueryCanceled"}`
- **WHEN** the audit runs
- **THEN** exactly one maintenance recommendation `MAINTENANCE_OUTPUT_UNAVAILABLE` (warning) is emitted, existing recommendation codes are unchanged, and `main()` returns 0

#### Scenario: Zero-statistics table is visible but not alarming
- **GIVEN** `core.basin` with `relpages = 0`, `reltuples = -1`, `n_live_tup = 18`, 19 modifications and no analyze timestamps
- **WHEN** recommendations are built
- **THEN** exactly a `TABLE_ZERO_STATISTICS` info names it and no warning/critical is emitted for it

#### Scenario: Compressed chunk phantom counters are excluded
- **GIVEN** a chunk with `reloptions = {autovacuum_enabled=false}` and 191 million stale `n_dead_tup`
- **WHEN** the probe runs against a real database
- **THEN** that relation is absent from `maintenance_output` rows

