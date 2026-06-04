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
