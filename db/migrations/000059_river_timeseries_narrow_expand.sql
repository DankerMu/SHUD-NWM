-- I7: atomic, restartable river narrow-store expand (one statement).
-- No chunk is decompressed. 000047 is not re-run: its settings belong to
-- the renamed legacy table; this migration owns the new table's settings.
DO $$
DECLARE
    drift_count integer;
BEGIN
    IF to_regclass('hydro.river_timeseries_legacy') IS NULL THEN
        ALTER TABLE hydro.river_timeseries RENAME TO river_timeseries_legacy;
    END IF;

CREATE TABLE IF NOT EXISTS hydro.river_timeseries (
    run_key INTEGER NOT NULL REFERENCES hydro.hydro_run(run_key),
    basin_version_key INTEGER NOT NULL,
    river_network_version_key INTEGER NOT NULL,
    river_segment_key INTEGER NOT NULL REFERENCES core.river_segment(river_segment_key),
    valid_time TIMESTAMPTZ NOT NULL,
    lead_time_hours INTEGER,
    variable_e hydro.river_variable NOT NULL,
    value DOUBLE PRECISION NOT NULL,
    unit_e hydro.river_unit NOT NULL,
    quality_flag_e hydro.river_quality_flag NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT river_timeseries_narrow_pkey
        PRIMARY KEY (run_key, river_segment_key, variable_e, valid_time)
);

PERFORM create_hypertable('hydro.river_timeseries', 'valid_time',
    chunk_time_interval => interval '1 day', create_default_indexes => false,
    if_not_exists => true);

CREATE INDEX IF NOT EXISTS river_ts_segment_time_key_idx
    ON hydro.river_timeseries (river_segment_key, variable_e, valid_time DESC);
CREATE INDEX IF NOT EXISTS river_ts_run_discovery_key_idx
    ON hydro.river_timeseries
    (run_key, basin_version_key, river_network_version_key, variable_e, valid_time DESC);

    WITH live AS (
        SELECT attname::text AS attname,
               segmentby_column_index::int AS segmentby_column_index,
               orderby_column_index::int AS orderby_column_index,
               orderby_asc::boolean AS orderby_asc,
               orderby_nullsfirst::boolean AS orderby_nullsfirst
        FROM timescaledb_information.compression_settings
        WHERE hypertable_schema = 'hydro' AND hypertable_name = 'river_timeseries'
    ), expected AS (
        SELECT * FROM (VALUES
            ('run_key'::text, 1::int, NULL::int, NULL::boolean, NULL::boolean),
            ('river_segment_key'::text, 2::int, NULL::int, NULL::boolean, NULL::boolean),
            ('variable_e'::text, NULL::int, 1::int, true::boolean, false::boolean),
            ('valid_time'::text, NULL::int, 2::int, true::boolean, false::boolean)
        ) AS t(attname, segmentby_column_index, orderby_column_index, orderby_asc, orderby_nullsfirst)
    )
    SELECT count(*) INTO drift_count FROM (
        (SELECT * FROM live EXCEPT SELECT * FROM expected)
        UNION ALL
        (SELECT * FROM expected EXCEPT SELECT * FROM live)
    ) AS mismatch;
    IF drift_count <> 0 THEN
    ALTER TABLE hydro.river_timeseries SET (
        timescaledb.compress = true,
        timescaledb.compress_segmentby = 'run_key, river_segment_key',
        timescaledb.compress_orderby = 'variable_e, valid_time'
    );
    END IF;

-- Fail closed if deployment has not provisioned its runtime owner.
ALTER TABLE hydro.river_timeseries OWNER TO nhms_ingest_rw;

-- Classify only on installation; replay must preserve newly parsed narrow runs.
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'hydro' AND table_name = 'hydro_run'
          AND column_name = 'timeseries_store'
    ) THEN
        ALTER TABLE hydro.hydro_run ADD COLUMN timeseries_store TEXT NOT NULL DEFAULT 'narrow'
            CHECK (timeseries_store IN ('legacy', 'narrow'));
        UPDATE hydro.hydro_run SET timeseries_store = 'legacy'
        WHERE parsed_at IS NOT NULL OR status IN ('parsed', 'published');
    END IF;
END;
$$;
