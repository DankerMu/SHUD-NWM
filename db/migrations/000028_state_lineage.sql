-- M24 §2 Lane 1: warm-start lineage columns on hydro.state_snapshot.
-- All columns are NULLable with default NULL so existing rows are untouched and
-- pre-Lane-1 producers (which do not supply lineage) keep working.

ALTER TABLE hydro.state_snapshot
  ADD COLUMN IF NOT EXISTS source_id TEXT DEFAULT NULL;

ALTER TABLE hydro.state_snapshot
  ADD COLUMN IF NOT EXISTS cycle_id TEXT DEFAULT NULL;

ALTER TABLE hydro.state_snapshot
  ADD COLUMN IF NOT EXISTS lead_hours INTEGER DEFAULT NULL;

ALTER TABLE hydro.state_snapshot
  ADD COLUMN IF NOT EXISTS model_package_version TEXT DEFAULT NULL;

ALTER TABLE hydro.state_snapshot
  ADD COLUMN IF NOT EXISTS model_package_checksum TEXT DEFAULT NULL;

ALTER TABLE hydro.state_snapshot
  ADD COLUMN IF NOT EXISTS original_shud_filename TEXT DEFAULT NULL;

-- Forecast warm-start state is source-specific: a GFS-driven SHUD state and an
-- IFS-driven SHUD state at the same model/valid_time must not overwrite each
-- other. Pre-lineage rows with NULL source_id keep the legacy single-row
-- identity through the same NULL-aware unique index.
ALTER TABLE hydro.state_snapshot
  DROP CONSTRAINT IF EXISTS state_snapshot_model_id_valid_time_key;

CREATE UNIQUE INDEX IF NOT EXISTS state_snapshot_model_source_valid_time_key
  ON hydro.state_snapshot (model_id, COALESCE(source_id, ''), valid_time);
