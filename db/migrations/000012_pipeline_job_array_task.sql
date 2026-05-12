DO $$
BEGIN
  ALTER TABLE ops.pipeline_job
    ADD COLUMN IF NOT EXISTS array_task_id INT;
END $$;

CREATE INDEX IF NOT EXISTS pipeline_job_array_task_idx
  ON ops.pipeline_job (slurm_job_id, array_task_id)
  WHERE slurm_job_id IS NOT NULL AND array_task_id IS NOT NULL;
