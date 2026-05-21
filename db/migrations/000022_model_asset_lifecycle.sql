ALTER TABLE core.model_instance
  ADD COLUMN IF NOT EXISTS lifecycle_state TEXT NOT NULL DEFAULT 'inactive';

UPDATE core.model_instance
SET lifecycle_state = CASE WHEN active_flag THEN 'active' ELSE lifecycle_state END
WHERE lifecycle_state = 'inactive';

DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM core.model_instance
    WHERE lifecycle_state NOT IN ('inactive', 'active', 'deprecated', 'superseded')
  ) THEN
    RAISE EXCEPTION 'Invalid model_instance lifecycle_state rows exist before M18 constraint';
  END IF;
END $$;

ALTER TABLE core.model_instance
  DROP CONSTRAINT IF EXISTS model_instance_lifecycle_state_chk;

ALTER TABLE core.model_instance
  ADD CONSTRAINT model_instance_lifecycle_state_chk
  CHECK (lifecycle_state IN ('inactive', 'active', 'deprecated', 'superseded'));

DO $$
BEGIN
  IF EXISTS (
    SELECT basin_version_id
    FROM core.model_instance
    WHERE active_flag = true OR lifecycle_state = 'active'
    GROUP BY basin_version_id
    HAVING COUNT(*) > 1
  ) THEN
    RAISE EXCEPTION 'Multiple active model_instance rows exist for a basin_version_id before M18 uniqueness';
  END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS model_instance_active_basin_version_uidx
  ON core.model_instance (basin_version_id)
  WHERE active_flag = true AND lifecycle_state = 'active';
