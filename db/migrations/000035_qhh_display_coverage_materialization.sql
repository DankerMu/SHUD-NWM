-- M-node27: materialize per-run QHH display coverage so the latest-product
-- readiness path can serve from a cheap run_id JOIN instead of recomputing the
-- station/river coverage CTEs (forcing_station_timeseries + river_timeseries
-- scanned and re-aggregated by 15+ CTEs) on every request.
--
-- Columns mirror, field-for-field, the coverage columns the candidate query's
-- final SELECT derives from station_coverage (sc), station_variable_coverage
-- (svc) and hydro_coverage (hc). The refresh (packages/common/display_coverage.py)
-- recomputes them with the identical CTE arithmetic, so the cheap path is a
-- byte-for-byte stand-in for the CTE path.
--
-- This table is OPTIONAL: forecast_store only uses it when it exists AND has a
-- row for the run. node-22 (no migration applied) keeps the CTE path unchanged.

CREATE TABLE IF NOT EXISTS hydro.run_display_coverage (
  run_id                     TEXT PRIMARY KEY REFERENCES hydro.hydro_run(run_id) ON DELETE CASCADE,

  -- station_coverage (sc)
  station_count              INTEGER NOT NULL DEFAULT 0 CHECK (station_count >= 0),
  station_sample_count       BIGINT  NOT NULL DEFAULT 0 CHECK (station_sample_count >= 0),
  station_source_id          TEXT,
  station_display_start_time TIMESTAMPTZ,
  station_display_end_time   TIMESTAMPTZ,
  station_valid_time_start   TIMESTAMPTZ,
  station_valid_time_end     TIMESTAMPTZ,

  -- station_variable_coverage (svc) -- the per-variable jsonb array, verbatim
  station_variable_coverage  JSONB   NOT NULL DEFAULT '[]'::jsonb,

  -- hydro_coverage (hc)
  segment_count              INTEGER NOT NULL DEFAULT 0 CHECK (segment_count >= 0),
  river_sample_count         BIGINT  NOT NULL DEFAULT 0 CHECK (river_sample_count >= 0),
  river_valid_time_start     TIMESTAMPTZ,
  river_valid_time_end       TIMESTAMPTZ,
  min_lead_time_hours        INTEGER,
  max_lead_time_hours        INTEGER,

  refreshed_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_at                 TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE hydro.run_display_coverage IS
  'Per-run materialized QHH display coverage (station/river counts + windows + '
  'per-variable jsonb). Cheap stand-in for the latest-product coverage CTEs; '
  'optional and node-27-local (node-22 keeps the CTE path).';
