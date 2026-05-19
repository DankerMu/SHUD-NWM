DO $$
DECLARE
  current_primary_key TEXT;
BEGIN
  SELECT pg_get_constraintdef(c.oid)
  INTO current_primary_key
  FROM pg_constraint c, pg_class t, pg_namespace n
  WHERE t.oid = c.conrelid
    AND n.oid = t.relnamespace
    AND n.nspname = 'flood'
    AND t.relname = 'return_period_result'
    AND c.conname = 'return_period_result_pkey'
    AND c.contype = 'p';

  IF current_primary_key IS DISTINCT FROM
     'PRIMARY KEY (run_id, river_network_version_id, river_segment_id, duration, valid_time)' THEN
    ALTER TABLE flood.return_period_result DROP CONSTRAINT IF EXISTS return_period_result_pkey;
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint c, pg_class t, pg_namespace n
    WHERE t.oid = c.conrelid
      AND n.oid = t.relnamespace
      AND n.nspname = 'flood'
      AND t.relname = 'return_period_result'
      AND c.conname = 'return_period_result_pkey'
      AND c.contype = 'p'
  ) THEN
    ALTER TABLE flood.return_period_result
      ADD CONSTRAINT return_period_result_pkey
      PRIMARY KEY (run_id, river_network_version_id, river_segment_id, duration, valid_time);
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS return_period_result_summary_idx
  ON flood.return_period_result (run_id, max_over_window, quality_flag, warning_level)
  WHERE warning_level IS NOT NULL;

CREATE INDEX IF NOT EXISTS return_period_result_ranking_idx
  ON flood.return_period_result (
    run_id,
    max_over_window,
    quality_flag,
    return_period DESC NULLS LAST,
    q_value DESC,
    river_network_version_id,
    river_segment_id
  );

CREATE INDEX IF NOT EXISTS return_period_result_timeline_idx
  ON flood.return_period_result (
    run_id,
    river_network_version_id,
    river_segment_id,
    max_over_window,
    valid_time
  );

CREATE INDEX IF NOT EXISTS return_period_result_map_idx
  ON flood.return_period_result (
    run_id,
    duration,
    valid_time,
    return_period DESC NULLS LAST,
    quality_flag,
    river_network_version_id,
    river_segment_id
  );
