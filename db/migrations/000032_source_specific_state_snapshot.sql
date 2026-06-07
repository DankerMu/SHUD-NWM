-- Forecast warm-start state is source-specific. GFS-driven and IFS-driven
-- SHUD states at the same model/valid_time must not overwrite each other.

ALTER TABLE hydro.state_snapshot
  DROP CONSTRAINT IF EXISTS state_snapshot_model_id_valid_time_key;

DROP INDEX IF EXISTS hydro.state_snapshot_model_source_valid_time_key;

CREATE UNIQUE INDEX IF NOT EXISTS state_snapshot_model_source_valid_time_key
  ON hydro.state_snapshot (model_id, (COALESCE(source_id, ''::text)), valid_time);
