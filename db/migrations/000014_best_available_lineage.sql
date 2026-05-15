ALTER TABLE met.best_available_selection
  ADD COLUMN IF NOT EXISTS forcing_version_id TEXT NOT NULL DEFAULT 'legacy_global';

ALTER TABLE met.best_available_selection
  DROP CONSTRAINT IF EXISTS best_available_selection_valid_time_variable_key;

CREATE UNIQUE INDEX IF NOT EXISTS best_available_selection_forcing_time_variable_idx
  ON met.best_available_selection (forcing_version_id, valid_time, variable);
