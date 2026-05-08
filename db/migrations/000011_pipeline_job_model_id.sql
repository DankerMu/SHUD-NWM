DO $$
BEGIN
  ALTER TABLE ops.pipeline_job
    ADD COLUMN IF NOT EXISTS model_id TEXT;
END $$;
