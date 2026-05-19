DO $$
DECLARE
  current_primary_key TEXT;
  duplicate_key RECORD;
BEGIN
  UPDATE flood.return_period_result
  SET max_over_window = false
  WHERE max_over_window IS NULL;

  ALTER TABLE flood.return_period_result
    ALTER COLUMN max_over_window SET DEFAULT false,
    ALTER COLUMN max_over_window SET NOT NULL;

  SELECT
    run_id,
    river_network_version_id,
    river_segment_id,
    duration,
    valid_time,
    max_over_window,
    COUNT(*) AS row_count
  INTO duplicate_key
  FROM flood.return_period_result
  GROUP BY run_id, river_network_version_id, river_segment_id, duration, valid_time, max_over_window
  HAVING COUNT(*) > 1
  ORDER BY row_count DESC, run_id, river_network_version_id, river_segment_id, duration, valid_time, max_over_window
  LIMIT 1;

  IF FOUND THEN
    RAISE EXCEPTION USING
      ERRCODE = 'unique_violation',
      MESSAGE = 'Cannot upgrade flood.return_period_result primary key: duplicate max-over-window return-period rows exist.',
      DETAIL = format(
        'Duplicate key (run_id, river_network_version_id, river_segment_id, duration, valid_time, max_over_window)=(%s, %s, %s, %s, %s, %s) has %s rows.',
        duplicate_key.run_id,
        duplicate_key.river_network_version_id,
        duplicate_key.river_segment_id,
        duplicate_key.duration,
        duplicate_key.valid_time,
        duplicate_key.max_over_window,
        duplicate_key.row_count
      ),
      HINT = 'Deduplicate or quarantine duplicate return-period rows before applying migration 000017.';
  END IF;

  SELECT pg_get_constraintdef(c.oid)
  INTO current_primary_key
  FROM pg_constraint c, pg_class t, pg_namespace n
  WHERE n.nspname = 'flood'
    AND n.oid = t.relnamespace
    AND t.oid = c.conrelid
    AND t.relname = 'return_period_result'
    AND c.conname = 'return_period_result_pkey'
    AND c.contype = 'p';

  IF current_primary_key IS DISTINCT FROM
     'PRIMARY KEY (run_id, river_network_version_id, river_segment_id, duration, valid_time, max_over_window)' THEN
    ALTER TABLE flood.return_period_result DROP CONSTRAINT IF EXISTS return_period_result_pkey;
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint c, pg_class t, pg_namespace n
    WHERE n.nspname = 'flood'
      AND n.oid = t.relnamespace
      AND t.oid = c.conrelid
      AND t.relname = 'return_period_result'
      AND c.conname = 'return_period_result_pkey'
      AND c.contype = 'p'
  ) THEN
    ALTER TABLE flood.return_period_result
      ADD CONSTRAINT return_period_result_pkey
      PRIMARY KEY (run_id, river_network_version_id, river_segment_id, duration, valid_time, max_over_window);
  END IF;
END $$;
