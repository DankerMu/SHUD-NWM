DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'interp_weight_direct_grid_exact_weight_chk'
      AND conrelid = 'met.interp_weight'::regclass
  ) THEN
    ALTER TABLE met.interp_weight
      ADD CONSTRAINT interp_weight_direct_grid_exact_weight_chk
      CHECK (method <> 'direct_grid' OR weight = 1.0);
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'interp_weight_direct_grid_signature_chk'
      AND conrelid = 'met.interp_weight'::regclass
  ) THEN
    ALTER TABLE met.interp_weight
      ADD CONSTRAINT interp_weight_direct_grid_signature_chk
      CHECK (method <> 'direct_grid' OR NULLIF(BTRIM(grid_signature), '') IS NOT NULL);
  END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS interp_weight_direct_grid_station_variable_uidx
  ON met.interp_weight (source_id, grid_id, model_id, station_id, variable)
  WHERE method = 'direct_grid';
