DO $$
BEGIN
  ALTER TABLE ops.pipeline_job
    ADD COLUMN IF NOT EXISTS model_id TEXT;
END $$;

CREATE INDEX IF NOT EXISTS pipeline_job_slurm_job_idx
  ON ops.pipeline_job (slurm_job_id)
  WHERE slurm_job_id IS NOT NULL;
