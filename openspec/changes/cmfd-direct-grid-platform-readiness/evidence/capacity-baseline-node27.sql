-- §2.6 G9 Capacity Baseline queries for node-27 active primary PG.
--
-- Purpose: measure live legacy baseline at evidence-production time (basin
-- count, legacy station count, met.forcing_station_timeseries row counts)
-- so §2.6 report can cite exact SQL + measurement timestamps rather than
-- appendix-A prior-day cross-checks alone.
--
-- Usage:
--   ssh -p 32099 nwm@210.77.77.27
--   cd /home/nwm/NWM && git pull --ff-only
--   set -a; source infra/env/node27-ingest.env; set +a
--   psql "$DATABASE_URL" \
--     -c "SET default_transaction_read_only = on; SET statement_timeout = '60s';" \
--     -f openspec/changes/cmfd-direct-grid-platform-readiness/evidence/capacity-baseline-node27.sql
--
-- Note: the display-ro env template (node27-display-ro.env) is not yet provisioned on
-- node-27; ingest.env is sufficient because this helper issues SELECT-only queries and
-- the psql -c preamble installs a session-level read-only guard + 60s statement timeout.
-- All queries here are SELECT-only; no DDL/DML. Row-count queries on
-- met.forcing_station_timeseries may take 5-30s on the hypertable — do NOT wrap in a
-- long-running transaction.
--
-- Cross-check reference (appendix-A of docs/ForcingReplace/CMFD 建模资产向
-- IFSGFS Direct-Grid 的安全迁移.md, 2026-07-06 snapshot): 13 active basins,
-- 6,290 active legacy stations, ~121M met.forcing_station_timeseries rows
-- per 2 weeks (~8M rows/day). These are cross-check references only, NOT
-- acceptance values — §2.6 accepts what the SQL below returns at run time.

\echo ===== §2.6 G9 Capacity Baseline (node-27, live SQL) =====
\echo audit UTC:
SELECT now() AT TIME ZONE 'UTC' AS audit_utc;

\echo
\echo ---- Q1a. Active model_instance count (production basin oracle; expected ~13 per appendix-A) ----
\echo (matches smoke-2.4.node-27.pass.log INV.A count=13; the correct
\echo  "production basin count" query on node-27 is model_instance active,
\echo  not basin_version — basin_version rows all carry active_flag=false
\echo  because they are versioning bookkeeping rows.)
SELECT count(*) AS active_model_instance_count
  FROM core.model_instance
  WHERE active_flag = true;

\echo ---- Q1b. Total basin/basin_version count (13 prod + 1 evidence-only) ----
SELECT count(*) AS total_basin_count FROM core.basin;
SELECT count(*) AS active_basin_version_count
  FROM core.basin_version
  WHERE active_flag = true;
SELECT count(*) AS total_basin_version_count
  FROM core.basin_version;

\echo
\echo ---- Q2. Active legacy met_station count (expected ~6,290 per appendix-A) ----
SELECT count(*) AS active_met_station_count
  FROM met.met_station
  WHERE active_flag = true;

\echo ---- Q2'. Total met_station count (production + any evidence-only mirrors) ----
SELECT count(*) AS total_met_station_count
  FROM met.met_station;

\echo
\echo ---- Q3. met.forcing_station_timeseries recent 2-week row count ----
\echo (cross-check against appendix-A ~121M rows/2wks ≈ 8M rows/day)
SELECT count(*) AS forcing_station_timeseries_rows_2wk
  FROM met.forcing_station_timeseries
  WHERE valid_time >= (now() - interval '2 weeks');

\echo
\echo ---- Q3'. Per-day rate approximation over the 2-week window (actual span from min/max) ----
\echo (span computed from min/max valid_time, NOT hardcoded 14 — protects against a
\echo  partially-populated window when ingest just started or the hypertable was truncated)
SELECT
  count(*)                                                                              AS window_row_count,
  min(valid_time)                                                                       AS window_min_valid_time,
  max(valid_time)                                                                       AS window_max_valid_time,
  EXTRACT(EPOCH FROM (max(valid_time) - min(valid_time))) / 86400.0                     AS actual_span_days,
  CASE WHEN max(valid_time) > min(valid_time)
       THEN count(*)::numeric / (EXTRACT(EPOCH FROM (max(valid_time) - min(valid_time))) / 86400.0)
       ELSE NULL
  END                                                                                   AS approx_rows_per_day
  FROM met.forcing_station_timeseries
  WHERE valid_time >= (now() - interval '2 weeks');

\echo
\echo ---- Q4. Recent-2wk row breakdown by variable (formula validation) ----
SELECT
  variable,
  count(*) AS row_count
  FROM met.forcing_station_timeseries
  WHERE valid_time >= (now() - interval '2 weeks')
  GROUP BY variable
  ORDER BY variable;

\echo
\echo ---- Q5. Model_instance identity md5 (cross-check against smoke-2.4 INV.A' e95e51dd…) ----
SELECT
  count(*) AS active_mi_count,
  md5(coalesce(string_agg(model_id, ',' ORDER BY model_id), '')) AS active_mi_md5
  FROM core.model_instance
  WHERE active_flag = true;

\echo ===== end capacity baseline =====
